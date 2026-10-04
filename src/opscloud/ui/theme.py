"""OpsCloud brand colors and semantic constants for the TUI.

Single source of truth for color values used in Python code (Rich markup,
`Content.styled`, `Content.from_markup`). CSS-side styling references
Textual CSS variables set via `register_theme()` in `OpsCloudApp.__init__`.

App-specific variables (`$mode-bash`, `$mode-command`, `$mode-incognito`, `$skill`,
`$skill-hover`, `$tool`, `$tool-hover`, `$plan-add`, `$plan-destroy`, `$ctx-prod`, etc.)
are backed by these constants.
"""

from __future__ import annotations

import functools
from opscloud.utils.logger import get_logger
import os
import re
import tomllib
from dataclasses import dataclass, fields

from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from textual.app import App

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Brand palette — dark (Tokyo Night base + OpsCloud teal identity)
# ---------------------------------------------------------------------------
DC_DARK = "#11121D"
"""Background — Tokyo Night base."""

DC_CARD = "#1A1B2E"
"""Surface / card — elevated above background."""

DC_BORDER_DK = "#25283B"
"""Borders on dark backgrounds."""

DC_BORDER_LT = "#3B3E52"
"""Borders on lighter / hovered backgrounds."""

DC_BODY = "#C0CAF5"
"""Body text — Tokyo Night foreground."""

DC_TEAL = "#2dd4bf"
"""Primary accent teal — OpsCloud identity."""

DC_CYAN = "#7AA2F7"
"""Secondary accent — Tokyo Night blue."""

DC_PURPLE = "#BB9AF7"
"""Skills and knowledge packs — Tokyo Night purple."""

DC_GREEN = "#9ECE6A"
"""Success / positive — Tokyo Night green."""

DC_AMBER = "#e2b340"
"""Warning / tool operations indicator."""

DC_RED = "#F7768E"
"""Error / destructive — Tokyo Night red."""

DC_MUTED = "#545C7E"
"""Muted / secondary text — Tokyo Night comment."""

DC_GREEN_BG = "#1D2D20"
"""Subtle green-tinted background for diff additions."""

DC_RED_BG = "#2D2030"
"""Subtle red-tinted background for diff removals / errors."""

DC_PANEL = "#1E2030"
"""Panel — section background."""

DC_SKILL = DC_PURPLE
"""Skill invocation accent."""

DC_SKILL_HOVER = "#c4b5fd"
"""Skill invocation hover."""

DC_TOOL = DC_AMBER
"""Tool call accent."""

DC_TOOL_HOVER = "#fbbf24"
"""Tool call hover."""

DC_INCOGNITO = "#545C7E"
"""Incognito shell accent — muted to convey privacy."""

# DevOps Specific Dark Tokens
DC_PLAN_ADD = "#10b981"
DC_PLAN_CHANGE = "#f59e0b"
DC_PLAN_DESTROY = "#ef4444"
DC_CTX_PROD = "#dc2626"
DC_CTX_NONPROD = "#2563eb"
DC_STATUS_OK = "#10b981"
DC_STATUS_WARN = "#f59e0b"
DC_STATUS_DANGER = "#ef4444"

# ---------------------------------------------------------------------------
# Brand palette — light
# ---------------------------------------------------------------------------
DC_LIGHT_BG = "#f6f8fa"
DC_LIGHT_CARD = "#ffffff"
DC_LIGHT_BORDER = "#d0d7de"
DC_LIGHT_BORDER_HVR = "#afb8c1"
DC_LIGHT_BODY = "#24292f"
DC_LIGHT_TEAL = "#0d9488"
DC_LIGHT_CYAN = "#0284c7"
DC_LIGHT_PURPLE = "#7c3aed"
DC_LIGHT_GREEN = "#059669"
DC_LIGHT_AMBER = "#d97706"
DC_LIGHT_RED = "#dc2626"
DC_LIGHT_MUTED = "#57606a"
DC_LIGHT_GREEN_BG = "#dafbe1"
DC_LIGHT_RED_BG = "#ffebe9"
DC_LIGHT_PANEL = "#f3f4f6"
DC_LIGHT_SKILL = DC_LIGHT_PURPLE
DC_LIGHT_SKILL_HOVER = "#6d28d9"
DC_LIGHT_TOOL = DC_LIGHT_AMBER
DC_LIGHT_TOOL_HOVER = "#b45309"
DC_LIGHT_INCOGNITO = "#0f766e"

DC_LIGHT_PLAN_ADD = "#059669"
DC_LIGHT_PLAN_CHANGE = "#d97706"
DC_LIGHT_PLAN_DESTROY = "#dc2626"
DC_LIGHT_CTX_PROD = "#b91c1c"
DC_LIGHT_CTX_NONPROD = "#1d4ed8"
DC_LIGHT_STATUS_OK = "#059669"
DC_LIGHT_STATUS_WARN = "#d97706"
DC_LIGHT_STATUS_DANGER = "#dc2626"

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """Complete set of semantic colors for one theme variant."""

    primary: str
    secondary: str
    accent: str
    panel: str
    success: str
    warning: str
    error: str
    muted: str
    mode_bash: str
    mode_command: str
    mode_incognito: str
    skill: str
    skill_hover: str
    tool: str
    tool_hover: str
    foreground: str
    background: str
    surface: str
    # DevOps specific
    plan_add: str
    plan_change: str
    plan_destroy: str
    ctx_prod: str
    ctx_nonprod: str
    status_ok: str
    status_warn: str
    status_danger: str

    def __post_init__(self) -> None:
        """Validate all color values are valid 6-digit hex strings."""
        for f in fields(self):
            val = getattr(self, f.name)
            if not isinstance(val, str) or not _HEX_RE.match(val):
                raise ValueError(
                    f"ThemeColors field '{f.name}' must be a 6-digit hex string "
                    f"(got {val!r})"
                )

    @classmethod
    def merged(
        cls, base: ThemeColors, overrides: dict[str, Any]
    ) -> ThemeColors:
        """Return a new ThemeColors overlaying valid field overrides onto base."""
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            val = overrides.get(f.name)
            if val is not None and isinstance(val, str) and _HEX_RE.match(val):
                kwargs[f.name] = val
            else:
                kwargs[f.name] = getattr(base, f.name)
        return cls(**kwargs)


DARK_COLORS = ThemeColors(
    primary=DC_TEAL,
    secondary=DC_CYAN,
    accent=DC_PURPLE,
    panel=DC_PANEL,
    success=DC_GREEN,
    warning=DC_AMBER,
    error=DC_RED,
    muted=DC_MUTED,
    mode_bash=DC_GREEN,
    mode_command=DC_PURPLE,
    mode_incognito=DC_INCOGNITO,
    skill=DC_SKILL,
    skill_hover=DC_SKILL_HOVER,
    tool=DC_TOOL,
    tool_hover=DC_TOOL_HOVER,
    foreground=DC_BODY,
    background=DC_DARK,
    surface=DC_CARD,
    plan_add=DC_PLAN_ADD,
    plan_change=DC_PLAN_CHANGE,
    plan_destroy=DC_PLAN_DESTROY,
    ctx_prod=DC_CTX_PROD,
    ctx_nonprod=DC_CTX_NONPROD,
    status_ok=DC_STATUS_OK,
    status_warn=DC_STATUS_WARN,
    status_danger=DC_STATUS_DANGER,
)

LIGHT_COLORS = ThemeColors(
    primary=DC_LIGHT_TEAL,
    secondary=DC_LIGHT_CYAN,
    accent=DC_LIGHT_PURPLE,
    panel=DC_LIGHT_PANEL,
    success=DC_LIGHT_GREEN,
    warning=DC_LIGHT_AMBER,
    error=DC_LIGHT_RED,
    muted=DC_LIGHT_MUTED,
    mode_bash=DC_LIGHT_RED,
    mode_command=DC_LIGHT_PURPLE,
    mode_incognito=DC_LIGHT_INCOGNITO,
    skill=DC_LIGHT_SKILL,
    skill_hover=DC_LIGHT_SKILL_HOVER,
    tool=DC_LIGHT_TOOL,
    tool_hover=DC_LIGHT_TOOL_HOVER,
    foreground=DC_LIGHT_BODY,
    background=DC_LIGHT_BG,
    surface=DC_LIGHT_CARD,
    plan_add=DC_LIGHT_PLAN_ADD,
    plan_change=DC_LIGHT_PLAN_CHANGE,
    plan_destroy=DC_LIGHT_PLAN_DESTROY,
    ctx_prod=DC_LIGHT_CTX_PROD,
    ctx_nonprod=DC_LIGHT_CTX_NONPROD,
    status_ok=DC_LIGHT_STATUS_OK,
    status_warn=DC_LIGHT_STATUS_WARN,
    status_danger=DC_LIGHT_STATUS_DANGER,
)


LANGCHAIN_DARK_COLORS = ThemeColors(
    primary="#7AA2F7",
    secondary="#BB9AF7",
    accent="#9ECE6A",
    panel="#25283B",
    success="#9ECE6A",
    warning="#EB8B46",
    error="#F7768E",
    muted="#545C7E",
    mode_bash="#F7768E",
    mode_command="#BB9AF7",
    mode_incognito="#2DD4BF",
    skill="#A78BFA",
    skill_hover="#C4B5FD",
    tool="#EB8B46",
    tool_hover="#FFCB91",
    foreground="#C0CAF5",
    background="#11121D",
    surface="#1A1B2E",
    plan_add="#9ECE6A",
    plan_change="#EB8B46",
    plan_destroy="#F7768E",
    ctx_prod="#F7768E",
    ctx_nonprod="#7AA2F7",
    status_ok="#9ECE6A",
    status_warn="#EB8B46",
    status_danger="#F7768E",
)

LANGCHAIN_LIGHT_COLORS = ThemeColors(
    primary="#2E5EAA",
    secondary="#7C3AED",
    accent="#3A7D0A",
    panel="#E0E1E6",
    success="#3A7D0A",
    warning="#B45309",
    error="#BE185D",
    muted="#6B7280",
    mode_bash="#BE185D",
    mode_command="#7C3AED",
    mode_incognito="#0F766E",
    skill="#7C3AED",
    skill_hover="#6D28D9",
    tool="#B45309",
    tool_hover="#78350F",
    foreground="#24283B",
    background="#F5F5F7",
    surface="#EAEAEE",
    plan_add="#3A7D0A",
    plan_change="#B45309",
    plan_destroy="#BE185D",
    ctx_prod="#BE185D",
    ctx_nonprod="#2E5EAA",
    status_ok="#3A7D0A",
    status_warn="#B45309",
    status_danger="#BE185D",
)


@dataclass(frozen=True, slots=True)
class ThemeEntry:
    """Metadata and color palette for a registered theme."""

    label: str
    dark: bool
    colors: ThemeColors
    custom: bool = False


_TEXTUAL_THEME_LABELS: dict[str, str] = {
    "textual-dark": "Textual Dark",
    "textual-light": "Textual Light",
    "ansi-dark": "Terminal ANSI Dark",
    "ansi-light": "Terminal ANSI Light",
    "catppuccin-frappe": "Catppuccin Frappé",
    "catppuccin-latte": "Catppuccin Latte",
    "catppuccin-macchiato": "Catppuccin Macchiato",
    "catppuccin-mocha": "Catppuccin Mocha",
    "dracula": "Dracula",
    "flexoki": "Flexoki",
    "gruvbox": "Gruvbox",
    "monokai": "Monokai",
    "nord": "Nord",
    "rose-pine": "Rosé Pine",
    "rose-pine-dawn": "Rosé Pine Dawn",
    "rose-pine-moon": "Rosé Pine Moon",
    "tokyo-night": "Tokyo Night",
}


_registry: dict[str, ThemeEntry] | None = None

_textual_colors_cache: dict[tuple[str, bool], ThemeColors] = {}
"""Cache of derived built-in `ThemeColors` keyed on `(theme name, dark)`.

Avoids re-running hex validations on every widget render for built-in themes.
Cleared by `reload_registry()`.
"""


def _load_user_themes() -> dict[str, ThemeEntry]:
    """Load user-defined themes from ~/.opscloud/config.toml [themes.<name>] sections."""
    user_themes: dict[str, ThemeEntry] = {}
    config_path = Path(os.path.expanduser("~/.opscloud/config.toml"))
    if not config_path.exists():
        return user_themes

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        themes_data = data.get("themes", {})
        if isinstance(themes_data, dict):
            for name, tdata in themes_data.items():
                if isinstance(tdata, dict):
                    raw_label = tdata.get("label")
                    label = str(raw_label) if raw_label is not None else name.replace("-", " ").title()
                    dark = bool(tdata.get("dark", True))
                    base = DARK_COLORS if dark else LIGHT_COLORS
                    colors_override = tdata.get("colors", {}) if isinstance(tdata.get("colors"), dict) else tdata
                    colors = ThemeColors.merged(base, colors_override)
                    user_themes[name] = ThemeEntry(label=label, dark=dark, colors=colors, custom=True)

    except Exception as e:
        logger.warning("Failed to load user themes from %s: %s", config_path, e)
    return user_themes


def get_registry() -> Mapping[str, ThemeEntry]:
    """Return immutable view of registered themes (built-in + Textual built-ins + user defined)."""
    global _registry
    if _registry is None:
        r: dict[str, ThemeEntry] = {
            "opscloud-dark": ThemeEntry(
                label="DevOps Dark", dark=True, colors=DARK_COLORS, custom=True
            ),
            "opscloud-light": ThemeEntry(
                label="DevOps Light", dark=False, colors=LIGHT_COLORS, custom=True
            ),
            "langchain": ThemeEntry(
                label="LangChain Dark", dark=True, colors=LANGCHAIN_DARK_COLORS, custom=True
            ),
            "langchain-light": ThemeEntry(
                label="LangChain Light", dark=False, colors=LANGCHAIN_LIGHT_COLORS, custom=True
            ),
        }


        try:
            from textual.theme import BUILTIN_THEMES

            for name, builtin in BUILTIN_THEMES.items():
                label = _TEXTUAL_THEME_LABELS.get(name) or name.replace("-", " ").title()
                r[name] = ThemeEntry(
                    label=label,
                    dark=builtin.dark,
                    colors=DARK_COLORS if builtin.dark else LIGHT_COLORS,
                    custom=False,
                )
        except ImportError:
            pass

        r.update(_load_user_themes())
        _registry = r

    return MappingProxyType(_registry)


def reload_registry() -> Mapping[str, ThemeEntry]:
    """Clear cache and rebuild theme registry."""
    global _registry
    _registry = None
    _textual_colors_cache.clear()
    return get_registry()


def register_app_themes(app: App[Any]) -> None:
    """Register custom OpsCloud and user-defined themes with Textual."""
    from textual.theme import Theme

    dark_theme = Theme(
        name="opscloud-dark",
        primary=DARK_COLORS.primary,
        secondary=DARK_COLORS.secondary,
        accent=DARK_COLORS.accent,
        foreground=DARK_COLORS.foreground,
        background=DARK_COLORS.background,
        surface=DARK_COLORS.surface,
        panel=DARK_COLORS.panel,
        warning=DARK_COLORS.warning,
        error=DARK_COLORS.error,
        success=DARK_COLORS.success,
        dark=True,
    )
    light_theme = Theme(
        name="opscloud-light",
        primary=LIGHT_COLORS.primary,
        secondary=LIGHT_COLORS.secondary,
        accent=LIGHT_COLORS.accent,
        foreground=LIGHT_COLORS.foreground,
        background=LIGHT_COLORS.background,
        surface=LIGHT_COLORS.surface,
        panel=LIGHT_COLORS.panel,
        warning=LIGHT_COLORS.warning,
        error=LIGHT_COLORS.error,
        success=LIGHT_COLORS.success,
        dark=False,
    )
    try:
        app.register_theme(dark_theme)
        app.register_theme(light_theme)
    except Exception:
        logger.warning("Failed registering default opscloud themes", exc_info=True)

    reg = get_registry()
    for name, entry in reg.items():
        if entry.custom and name not in ("opscloud-dark", "opscloud-light"):
            try:
                t = Theme(
                    name=name,
                    primary=entry.colors.primary,
                    secondary=entry.colors.secondary,
                    accent=entry.colors.accent,
                    foreground=entry.colors.foreground,
                    background=entry.colors.background,
                    surface=entry.colors.surface,
                    panel=entry.colors.panel,
                    warning=entry.colors.warning,
                    error=entry.colors.error,
                    success=entry.colors.success,
                    dark=entry.dark,
                )
                app.register_theme(t)
            except Exception:
                logger.warning("Failed to register custom theme '%s'", name, exc_info=True)


CONFIG_PATH = Path(os.path.expanduser("~/.opscloud/config.toml"))


def load_terminal_default() -> str | None:
    """Read saved terminal theme default for current TERM_PROGRAM if set."""
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    if not term_program or not CONFIG_PATH.exists():
        return None
    try:
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        terminal_themes = data.get("ui", {}).get("terminal_themes", {})
        if isinstance(terminal_themes, dict):
            mapped = terminal_themes.get(term_program)
            if isinstance(mapped, str) and mapped in get_registry():
                return mapped
    except Exception:
        pass
    return None


def load_theme_preference() -> str:
    """Resolve active theme preference from environment -> per-terminal default -> config -> default."""
    env_theme = os.environ.get("OPSCODE_THEME", "").strip()
    if env_theme and env_theme in get_registry():
        return env_theme

    term_default = load_terminal_default()
    if term_default:
        return term_default

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            theme = data.get("ui", {}).get("theme")
            if isinstance(theme, str) and theme in get_registry():
                return theme
        except Exception:
            pass
    return "opscloud-dark"


def _read_config_toml_data() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config_toml_data(data: dict[str, Any]) -> bool:
    import contextlib
    import tempfile
    import tomli_w

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(CONFIG_PATH)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
        return True
    except Exception:
        logger.exception("Failed writing config to %s", CONFIG_PATH)
        return False



def save_theme_preference(name: str, config_path: Path | None = None) -> bool:
    """Save global theme preference to ~/.opscloud/config.toml."""
    from opscloud.config.toml_config import save_theme_preference as _save
    return _save(name, config_path=config_path or CONFIG_PATH)


def save_terminal_theme_mapping(term_program: str, name: str) -> bool:
    """Save per-terminal default theme to ~/.opscloud/config.toml."""
    data = _read_config_toml_data()
    ui = data.setdefault("ui", {})
    if not isinstance(ui, dict):
        ui = {}
        data["ui"] = ui
    tt = ui.setdefault("terminal_themes", {})
    if not isinstance(tt, dict):
        tt = {}
        ui["terminal_themes"] = tt
    tt[term_program] = name
    return _write_config_toml_data(data)


def sync_terminal_background(app: App[Any]) -> None:
    """Optional sync of terminal background color."""
    pass


def get_css_variable_defaults(
    *, dark: bool = True, colors: ThemeColors | None = None
) -> dict[str, str]:
    """Return app-specific custom CSS variable defaults for the given theme.

    Most styling is handled by Textual's built-in CSS variables (`$primary`,
    `$text-muted`, `$error-muted`, etc.). This function only returns the
    app-specific semantic variables that have no Textual equivalent.

    Args:
        dark: Selects ``DARK_COLORS`` or ``LIGHT_COLORS`` when ``colors`` is None.
        colors: Explicit color set to use. Takes precedence over ``dark``.

    Returns:
        Dict of CSS variable names to hex color values.
    """
    c = colors if colors is not None else (DARK_COLORS if dark else LIGHT_COLORS)
    return {
        "mode-bash": c.mode_bash,
        "mode-command": c.mode_command,
        "mode-incognito": c.mode_incognito,
        "skill": c.skill,
        "skill-hover": c.skill_hover,
        "tool": c.tool,
        "tool-hover": c.tool_hover,
        "plan-add": c.plan_add,
        "plan-change": c.plan_change,
        "plan-destroy": c.plan_destroy,
        "ctx-prod": c.ctx_prod,
        "ctx-nonprod": c.ctx_nonprod,
        "status-ok": c.status_ok,
        "status-warn": c.status_warn,
        "status-danger": c.status_danger,
    }



def _colors_from_textual_theme(app: object) -> ThemeColors:
    """Construct `ThemeColors` from the app's active Textual theme."""
    ct = app.current_theme  # type: ignore[attr-defined]
    dark: bool = ct.dark
    base = DARK_COLORS if dark else LIGHT_COLORS

    def _hex_or(val: str | None, fallback: str) -> str:
        if val is not None and _HEX_RE.match(val):
            return val
        return fallback

    return ThemeColors(
        primary=_hex_or(ct.primary, base.primary),
        secondary=_hex_or(ct.secondary, base.secondary),
        accent=_hex_or(ct.accent, base.accent),
        panel=_hex_or(ct.panel, base.panel),
        success=_hex_or(ct.success, base.success),
        warning=_hex_or(ct.warning, base.warning),
        error=_hex_or(ct.error, base.error),
        muted=base.muted,
        mode_bash=_hex_or(ct.error, base.mode_bash),
        mode_command=_hex_or(ct.secondary, base.mode_command),
        mode_incognito=base.mode_incognito,
        skill=base.skill,
        skill_hover=base.skill_hover,
        tool=_hex_or(ct.warning, base.tool),
        tool_hover=base.tool_hover,
        foreground=_hex_or(ct.foreground, base.foreground),
        background=_hex_or(ct.background, base.background),
        surface=_hex_or(ct.surface, base.surface),
        plan_add=base.plan_add,
        plan_change=base.plan_change,
        plan_destroy=base.plan_destroy,
        ctx_prod=base.ctx_prod,
        ctx_nonprod=base.ctx_nonprod,
        status_ok=base.status_ok,
        status_warn=base.status_warn,
        status_danger=base.status_danger,
    )


def get_theme_colors(widget_or_app: object = None) -> ThemeColors:
    """Return the ``ThemeColors`` for the active Textual theme.

    For custom themes (OpsCloud-branded and user-defined), the pre-built
    ``ThemeColors`` from the registry is returned directly.  For Textual
    built-in themes, colors are resolved dynamically from the actual theme
    properties so Python-side styling stays in sync with CSS variables.

    Args:
        widget_or_app: Textual ``App``, a mounted widget, or ``None``.

    Returns:
        ``ThemeColors`` for the active theme.
    """
    if widget_or_app is None:
        # Fall back to the active Textual app context var when no explicit
        # widget/app is passed.
        try:
            from textual._context import active_app  # noqa: PLC2701

            widget_or_app = active_app.get()
        except (ImportError, LookupError):
            return DARK_COLORS

    app = getattr(widget_or_app, "app", widget_or_app)
    entry = get_registry().get(getattr(app, "theme", None))  # type: ignore[arg-type]

    # Custom themes (OpsCloud-branded / user-defined) use pre-built colors —
    # no need to derive from Textual's resolved theme properties.
    if entry is not None and entry.custom:
        return entry.colors

    # Built-in or unrecognized themes — derive from the resolved Textual
    # theme so Python styling matches CSS variables. Cache registered built-ins.
    try:
        ct = app.current_theme  # type: ignore[attr-defined]
        if entry is None:
            # Unregistered runtime theme — derive but don't cache.
            return _colors_from_textual_theme(app)
        key = (getattr(app, "theme", ""), bool(ct.dark))  # type: ignore[attr-defined]
        colors = _textual_colors_cache.get(key)
        if colors is None:
            colors = _colors_from_textual_theme(app)
            _textual_colors_cache[key] = colors
        return colors
    except Exception:
        logger.warning("Could not resolve theme colors dynamically", exc_info=True)
        if entry is not None:
            return entry.colors
        return DARK_COLORS



