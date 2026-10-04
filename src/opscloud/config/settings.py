"""Runtime settings state, environment detection, and bootstrap for OpsCloud.

The ``Settings`` dataclass holds all runtime configuration.
It can be hydrated from:
1. **ConfigStore** (primary when DB is available): ``Settings.from_store(store)``
   resolves settings through the DB → env → TOML → manifest defaults chain.
2. **Environment & TOML** (bootstrap fallback): ``Settings.from_env()`` for early
   startup and CLI initialization before the store is initialized.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
from opscloud.utils.logger import get_logger
import os
from pathlib import Path
import sys
import threading
from typing import Any

from opscloud.config import paths
from opscloud.config.manifest import (
    get_config_options,
    load_config_toml,
    resolve_scalar,
)
from opscloud.config.paths import (
    CONFIG_PATH,
    DATA_DIR,
    DEFAULT_AGENT_NAME,
    DOTENV_DENIED_ENV_KEYS,
    ENV_PREFIX,
    RELOADABLE_FIELDS,
    find_project_root,
)

logger = get_logger(__name__)


# ── Bootstrap State ──────────────────────────────────────


@dataclass
class _BootstrapState:
    """Internal state tracker for settings initialization lifecycle."""

    done: bool = False
    start_path: Path | None = None


_bootstrap_state = _BootstrapState()
_bootstrap_lock = threading.Lock()


def sync_aws_env_aliases() -> None:
    """Harmonize environment variable aliases for AWS CLI and SDK compatibility.

    Ensures bidirectional synchronization between standard AWS config keys:
    - AWS_REGION <-> AWS_DEFAULT_REGION
    - AWS_PROFILE <-> AWS_DEFAULT_PROFILE
    - Ensures AWS_PAGER is set to empty to disable interactive paging in subprocesses.
    """
    # Region synchronization
    reg = os.environ.get("AWS_REGION")
    def_reg = os.environ.get("AWS_DEFAULT_REGION")
    if reg and not def_reg:
        os.environ["AWS_DEFAULT_REGION"] = reg
    elif def_reg and not reg:
        os.environ["AWS_REGION"] = def_reg

    # Profile synchronization
    prof = os.environ.get("AWS_PROFILE")
    def_prof = os.environ.get("AWS_DEFAULT_PROFILE")
    if prof and not def_prof:
        os.environ["AWS_DEFAULT_PROFILE"] = prof
    elif def_prof and not prof:
        os.environ["AWS_PROFILE"] = def_prof

    # Ensure non-interactive pager for AWS CLI
    if "AWS_PAGER" not in os.environ:
        os.environ["AWS_PAGER"] = ""


def _ensure_bootstrap() -> None:
    """One-time bootstrap: dotenv loading and env sync. Idempotent and thread-safe."""
    if _bootstrap_state.done:
        return
    with _bootstrap_lock:
        if _bootstrap_state.done:
            return
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            _bootstrap_state.start_path = ctx.user_cwd if ctx else None
            _load_dotenv(start_path=_bootstrap_state.start_path)
            sync_aws_env_aliases()
        except Exception:
            logger.exception("Bootstrap failed; proceeding with env as-is.")
        finally:
            _bootstrap_state.done = True


# ── Env-Var Resolution ───────────────────────────────────


def resolve_env_var(name: str, fallback_names: tuple[str, ...] = ()) -> str | None:
    """Resolve an environment variable with OPSCLOUD_ prefix precedence and Settings fallback.

    Precedence:
    1. OPSCLOUD_{name}
    2. {name}
    3. Fallback names in order
    4. Current Settings singleton attribute if populated
    """
    _ensure_bootstrap()
    prefixed = f"{ENV_PREFIX}{name}"
    val = os.environ.get(prefixed)
    if val is not None and val != "":
        return val

    val = os.environ.get(name)
    if val is not None and val != "":
        return val

    for alt in fallback_names:
        val = os.environ.get(alt)
        if val is not None and val != "":
            return val

    # Fallback to _settings singleton if populated
    if _settings is not None:
        field_name = name.lower()
        if hasattr(_settings, field_name):
            field_val = getattr(_settings, field_name)
            if field_val is not None and str(field_val).strip() != "":
                return str(field_val)

    return None


def _resolve_env_var_from(env: dict[str, str], name: str) -> str | None:
    """Helper to resolve env vars from a specific dictionary mapping."""
    prefixed = f"{ENV_PREFIX}{name}"
    val = env.get(prefixed)
    if val is not None and val != "":
        return val
    val = env.get(name)
    return val if val != "" else None


# ── Dotenv Loading ───────────────────────────────────────


def _load_dotenv(*, start_path: Path | None = None, refresh_loaded: bool = False) -> None:
    """Load .env files: project-level (walk-up), then global ~/.opscloud/.env."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return

    effective_start = start_path
    if effective_start is None:
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            if ctx is not None:
                effective_start = ctx.user_cwd
        except Exception:
            pass

    search = (effective_start or Path.cwd()).expanduser().resolve()
    project_env: Path | None = None
    for parent in [search, *search.parents]:
        candidate = parent / ".env"
        if candidate.is_file():
            project_env = candidate
            break

    loaded_vals: dict[str, str | None] = {}

    # 1. Global ~/.opscloud/.env
    global_env = paths.GLOBAL_ENV_PATH
    if global_env.is_file():
        with contextlib.suppress(Exception):
            loaded_vals.update(dotenv_values(global_env))

    # 2. Project/CWD .env (higher priority, overwrites global)
    if project_env:
        with contextlib.suppress(Exception):
            loaded_vals.update(dotenv_values(project_env))

    # Filter out denied keys
    for key in DOTENV_DENIED_ENV_KEYS:
        if key in loaded_vals:
            logger.warning("Denied .env key in dotenv file: %s", key)
            loaded_vals.pop(key, None)

    # Apply to os.environ
    for k, v in loaded_vals.items():
        if v is not None and (refresh_loaded or k not in os.environ):
            os.environ[k] = v


def parse_shell_allow_list(val: str | list[str] | None) -> list[str] | None:
    """Parse comma-separated or list representation of allowed shell commands."""
    if val is None:
        return None
    if isinstance(val, list):
        return [item.strip() for item in val if item.strip()]
    if not val.strip():
        return []
    return [cmd.strip() for cmd in val.split(",") if cmd.strip()]


# ── Settings Dataclass ───────────────────────────────────


@dataclass
class Settings:
    """Runtime configuration state for OpsCloud."""

    # ── Active Model State ────────────────────────────────
    model_name: str | None = None
    model_provider: str | None = None
    reasoning_effort: str | None = None
    model_context_limit: int | None = 128_000
    model_unsupported_modalities: frozenset[str] = field(default_factory=frozenset)

    # ── Assistant / Agent Identity ────────────────────────
    assistant_id: str = DEFAULT_AGENT_NAME

    # ── Security & Approvals ──────────────────────────────
    approval_mode: str = "manual"
    shell_allow_list: list[str] | None = None

    # ── Execution Sandbox ─────────────────────────────────
    sandbox_provider: str = "local"

    # ── Cloud Provider Configuration ──────────────────────
    cloud_provider: str = "aws"

    # ── AWS Cloud Configuration ───────────────────────────
    aws_region: str = "us-east-1"
    aws_profile: str = "default"
    aws_role_arn: str | None = None

    # ── Provider Credentials ──────────────────────────────
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    groq_api_key: str | None = None
    deepseek_api_key: str | None = None
    tavily_api_key: str | None = None
    azure_openai_api_key: str | None = None
    typesafe_api_key: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    # ── State Checkpointing & DB ──────────────────────────
    checkpoint_backend: str = "sqlite"
    postgres_uri: str | None = None

    # ── Tracing & Telemetry ───────────────────────────────
    langchain_api_key: str | None = None
    langchain_tracing: bool = False
    langchain_project: str = "opscloud"
    langchain_endpoint: str = "https://api.smith.langchain.com"
    user_langchain_project: str | None = "opscloud"

    # ── Interpreter & PTC Execution ───────────────────────
    enable_interpreter: bool = True
    interpreter_ptc: str | list[str] | None = "safe"
    interpreter_ptc_acknowledge_unsafe: bool = False
    interpreter_timeout_seconds: int = 30
    interpreter_memory_limit_mb: int = 64
    interpreter_max_ptc_calls: int = 50
    interpreter_max_result_chars: int = 50000

    # ── UI / Display ──────────────────────────────────────
    theme: str = "dark"
    show_timestamps: bool = True
    auto_scroll: bool = True
    notifications_enabled: bool = True
    show_turn_duration: bool = True
    verbose_output: bool = False
    auto_compact: bool = False
    show_scrollbar: bool = False
    auto_update: bool = True

    # ── System & Filesystem Paths ─────────────────────────
    config_dir: Path = DATA_DIR
    cwd: Path | None = None
    project_root: Path | None = None
    extra_skills_dirs: list[str] | str | None = None
    debug: bool = False
    log_level: str = "INFO"
    log_mode: str = "text"
    log_to_console: bool = False
    log_to_file: bool = True
    log_dir: Path = Path("/tmp/opscloud_logs")
    log_file: str = "opscloud.log"

    def __post_init__(self) -> None:
        if self.cwd is None or self.project_root is None:
            try:
                from opscloud.project_utils import get_server_project_context

                ctx = get_server_project_context()
                if ctx is not None:
                    if self.cwd is None:
                        self.cwd = ctx.user_cwd
                    if self.project_root is None:
                        self.project_root = ctx.project_root
            except Exception:
                pass

        if self.project_root is None and self.cwd is not None:
            self.project_root = find_project_root(self.cwd)

        if not self.langchain_project and self.user_langchain_project:
            self.langchain_project = self.user_langchain_project

    # ── Model Property Compatibility ──────────────────────

    @property
    def model(self) -> str | None:
        """Alias for model_name for parity with model selector."""
        return self.model_name

    @model.setter
    def model(self, value: str | None) -> None:
        self.model_name = value

    @property
    def config_path(self) -> Path:
        """Return the active configuration file path."""
        return paths.CONFIG_PATH

    @property
    def user_opscloud_dir(self) -> Path:
        return paths.DATA_DIR

    @property
    def has_openai(self) -> bool:
        return self.openai_api_key is not None

    @property
    def has_anthropic(self) -> bool:
        return self.anthropic_api_key is not None

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key or os.environ.get("TAVILY_API_KEY"))

    @property
    def effective_cwd(self) -> Path:
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            if ctx is not None and ctx.user_cwd is not None:
                return ctx.user_cwd
        except Exception:
            pass
        if self.cwd is not None:
            return self.cwd
        return Path.cwd().resolve()

    @property
    def effective_project_root(self) -> Path | None:
        if self.project_root is not None:
            return self.project_root
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
            if ctx is not None and ctx.project_root is not None:
                return ctx.project_root
        except Exception:
            pass
        return find_project_root(self.effective_cwd)

    # ── Display & Mutator Helpers ─────────────────────────

    def to_display_dict(self) -> dict[str, str]:
        """Return all non-None fields as a display-friendly dict."""
        import dataclasses

        result = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if value is not None:
                result[f.name] = str(value)
        return result

    def set_field(self, key: str, value: str) -> tuple[bool, str]:
        """Set a settings field by name with type coercion."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(self)}
        if key not in field_names:
            return False, f"Unknown setting: {key}. Available: {', '.join(sorted(field_names))}"

        field_obj = next(f for f in dataclasses.fields(self) if f.name == key)
        type_str = (
            field_obj.type
            if isinstance(field_obj.type, str)
            else getattr(field_obj.type, "__name__", str(field_obj.type))
        )

        try:
            if "bool" in type_str:
                coerced: Any = value.lower() in ("true", "1", "yes", "on")
            elif "int" in type_str:
                coerced = int(value)
            elif "float" in type_str:
                coerced = float(value)
            elif "Path" in type_str:
                coerced = Path(value).expanduser().resolve()
            elif "list" in type_str:
                coerced = [item.strip() for item in value.split(",") if item.strip()]
            else:
                coerced = value

            setattr(self, key, coerced)
            return True, f"Set {key} = {value}"
        except (ValueError, TypeError) as exc:
            return False, f"Invalid value for {key}: {exc}"

    def reset_field(self, key: str) -> tuple[bool, str]:
        """Reset a settings field to its default value."""
        import dataclasses

        field_names = {f.name: f for f in dataclasses.fields(self)}
        if key not in field_names:
            return False, f"Unknown setting: {key}"

        f = field_names[key]
        default = f.default if f.default is not dataclasses.MISSING else None
        setattr(self, key, default)
        return True, f"Reset {key} to default"

    # ── Path & Agent Helpers ──────────────────────────────

    def ensure_agent_dir(self, assistant_id: str | None = None) -> Path:
        target_id = assistant_id or self.assistant_id or DEFAULT_AGENT_NAME
        return paths.ensure_agent_dir(target_id)

    def get_user_agents_dir(self, assistant_id: str | None = None) -> Path:
        target_id = assistant_id or self.assistant_id or DEFAULT_AGENT_NAME
        return paths.user_agents_dir(target_id)

    def get_project_agents_dir(self) -> Path | None:
        root = self.effective_project_root
        return paths.project_agents_dir(root) if root else None

    def get_user_agent_md_path(self, assistant_id: str | None = None) -> Path:
        target_id = assistant_id or self.assistant_id or DEFAULT_AGENT_NAME
        return paths.user_agent_md(target_id)

    def get_user_skills_dir(self, assistant_id: str | None = None) -> Path:
        target_id = assistant_id or self.assistant_id or DEFAULT_AGENT_NAME
        return paths.user_skills_dir(target_id)

    def ensure_user_skills_dir(self, assistant_id: str | None = None) -> Path:
        d = self.get_user_skills_dir(assistant_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_project_skills_dir(self) -> Path | None:
        root = self.effective_project_root
        return paths.project_skills_dir(root) if root else None

    def ensure_project_skills_dir(self) -> Path | None:
        root = self.effective_project_root
        if not root:
            return None
        d = self.get_project_skills_dir()
        if d:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def get_extra_skills_dirs(self) -> list[Path]:
        if isinstance(self.extra_skills_dirs, list):
            return [Path(p).expanduser().resolve() for p in self.extra_skills_dirs]
        if isinstance(self.extra_skills_dirs, str):
            return [
                Path(p.strip()).expanduser().resolve()
                for p in self.extra_skills_dirs.split(",")
                if p.strip()
            ]
        return []

    # ── Factory & Hydration Methods ───────────────────────

    @classmethod
    async def from_store(cls, store: Any) -> Settings:
        """Create settings by resolving every manifest option through the ConfigStore."""
        kwargs: dict[str, Any] = {}
        for option in get_config_options():
            if option.settings_field:
                val, _ = await store.resolve(option)
                if val is not None:
                    kwargs[option.settings_field] = val

        if "project_root" not in kwargs or kwargs.get("project_root") is None:
            kwargs["project_root"] = find_project_root()

        return cls(**kwargs)

    @classmethod
    def from_env(cls, start_path: Path | None = None) -> Settings:
        """Create settings from environment variables and config.toml (bootstrap fallback)."""
        _ensure_bootstrap()
        try:
            from opscloud.project_utils import get_server_project_context

            ctx = get_server_project_context()
        except Exception:
            ctx = None

        effective_start = start_path or (ctx.user_cwd if ctx else None)
        root = ctx.project_root if (ctx and ctx.project_root) else find_project_root(effective_start)
        toml_data = load_config_toml(CONFIG_PATH)

        kwargs: dict[str, Any] = {"project_root": root}
        if ctx and ctx.user_cwd:
            kwargs["cwd"] = ctx.user_cwd

        for opt in get_config_options():
            if opt.settings_field:
                val, _ = resolve_scalar(opt, toml_data=toml_data)
                if val is not None:
                    kwargs[opt.settings_field] = val

        # Handle shell allow list if string
        if "shell_allow_list" in kwargs and isinstance(kwargs["shell_allow_list"], str):
            kwargs["shell_allow_list"] = parse_shell_allow_list(kwargs["shell_allow_list"])

        return cls(**kwargs)

    async def sync_to_store(self, store: Any) -> int:
        """Persist current settings values back to the ConfigStore."""
        written = 0
        for option in get_config_options():
            if option.settings_field:
                cur_val = getattr(self, option.settings_field, None)
                if cur_val is not None:
                    await store.set_typed(option, cur_val)
                    written += 1
        return written

    def reload_from_environment(self, start_path: Path | str | None = None) -> list[str]:
        """Hot-reload reloadable settings (API keys, project root, etc.)."""
        p = Path(start_path) if start_path else None
        _load_dotenv(start_path=p, refresh_loaded=True)
        sync_aws_env_aliases()

        previous = {field_name: getattr(self, field_name, None) for field_name in RELOADABLE_FIELDS}
        toml_data = load_config_toml(CONFIG_PATH)

        changes: list[str] = []
        for opt in get_config_options():
            if opt.settings_field and hasattr(self, opt.settings_field):
                val, _ = resolve_scalar(opt, toml_data=toml_data)
                if val is not None:
                    setattr(self, opt.settings_field, val)

        for field_name in RELOADABLE_FIELDS:
            old_val = previous.get(field_name)
            new_val = getattr(self, field_name, None)
            if old_val != new_val:
                if "api_key" in field_name or "secret" in field_name:
                    old_disp = "set" if old_val else "unset"
                    new_disp = "set" if new_val else "unset"
                else:
                    old_disp = str(old_val)
                    new_disp = str(new_val)
                changes.append(f"{field_name}: {old_disp} -> {new_disp}")

        return changes


# ── Lazy Singleton ───────────────────────────────────────

_settings: Settings | None = None
_settings_lock = threading.RLock()


def get_settings() -> Settings:
    """Return the global Settings instance."""
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment and config.toml."""
    global _settings
    with _settings_lock:
        _bootstrap_state.done = False
        _settings = Settings.from_env()
        sync_aws_env_aliases()
    return _settings


async def reload_from_store(store: Any) -> Settings:
    """Replace the global Settings instance with one hydrated from the store."""
    global _settings
    with _settings_lock:
        _settings = await Settings.from_store(store)
        sync_aws_env_aliases()

        from opscloud.config.manifest import get_config_options

        for option in get_config_options():
            if option.settings_field and option.effective_env_var:
                val = getattr(_settings, option.settings_field, None)
                if val is not None and str(val).strip() != "":
                    os.environ[option.effective_env_var] = str(val)

        from opscloud.config.langsmith import apply_tracing_settings

        apply_tracing_settings(_settings)
    return _settings


# Module-level settings export for direct access
settings: Settings = get_settings()


# ── UI Glyphs & Charset Helpers ──────────────────────────


@dataclass(frozen=True)
class Glyphs:
    tool_prefix: str = "⏺"
    ellipsis: str = "…"
    checkmark: str = "✓"
    error: str = "✗"
    circle_empty: str = "○"
    circle_filled: str = "●"
    output_prefix: str = "⎿"
    spinner_frames: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    pause: str = "⏸"
    newline: str = "⏎"
    warning: str = "⚠"
    question: str = "?"
    hourglass: str = "⏳"
    retry: str = "↻"
    arrow_up: str = "↑"
    arrow_down: str = "↓"
    bullet: str = "•"
    cursor: str = "›"
    disclosure_collapsed: str = "▸"
    disclosure_expanded: str = "▾"
    box_vertical: str = "│"
    box_horizontal: str = "─"
    box_double_horizontal: str = "═"
    gutter_bar: str = "▌"
    git_branch: str = "↗"
    cross: str = "✗"
    arrow_right: str = "➜"


UNICODE_GLYPHS = Glyphs(
    tool_prefix="⏺",
    ellipsis="…",
    checkmark="✓",
    error="✗",
    circle_empty="○",
    circle_filled="●",
    output_prefix="⎿",
    spinner_frames=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    pause="⏸",
    newline="⏎",
    warning="⚠",
    question="?",
    hourglass="⏳",
    retry="↻",
    arrow_up="↑",
    arrow_down="↓",
    bullet="•",
    cursor="›",
    disclosure_collapsed="▸",
    disclosure_expanded="▾",
    box_vertical="│",
    box_horizontal="─",
    box_double_horizontal="═",
    gutter_bar="▌",
    git_branch="↗",
    cross="✗",
    arrow_right="➜",
)

ASCII_GLYPHS = Glyphs(
    tool_prefix="(*)",
    ellipsis="...",
    checkmark="[OK]",
    error="[X]",
    circle_empty="[ ]",
    circle_filled="[*]",
    output_prefix="L",
    spinner_frames=("(-)", "(\\)", "(|)", "(/)"),
    pause="||",
    newline="\\n",
    warning="[!]",
    question="[?]",
    hourglass="[~]",
    retry="[R]",
    arrow_up="^",
    arrow_down="v",
    bullet="-",
    cursor=">",
    disclosure_collapsed=">",
    disclosure_expanded="v",
    box_vertical="|",
    box_horizontal="-",
    box_double_horizontal="=",
    gutter_bar="|",
    git_branch="git:",
    cross="[X]",
    arrow_right="->",
)

_glyphs_cache: Glyphs | None = None
_charset_mode_cache: CharsetMode | None = None


class CharsetMode(StrEnum):
    UNICODE = "unicode"
    ASCII = "ascii"
    AUTO = "auto"


def _compute_charset_mode() -> CharsetMode:
    env_mode = (resolve_env_var("UI_CHARSET_MODE") or "auto").lower()
    if env_mode == "unicode":
        return CharsetMode.UNICODE
    if env_mode == "ascii":
        return CharsetMode.ASCII
    encoding = getattr(sys.stdout, "encoding", "") or ""
    if "utf" in encoding.lower():
        return CharsetMode.UNICODE
    lang = os.environ.get("LANG", "") or os.environ.get("LC_ALL", "")
    if "utf" in lang.lower():
        return CharsetMode.UNICODE
    return CharsetMode.ASCII


def _detect_charset_mode() -> CharsetMode:
    global _charset_mode_cache
    if _charset_mode_cache is not None:
        return _charset_mode_cache
    _charset_mode_cache = _compute_charset_mode()
    return _charset_mode_cache


def get_glyphs() -> Glyphs:
    global _glyphs_cache
    if _glyphs_cache is not None:
        return _glyphs_cache
    mode = _detect_charset_mode()
    _glyphs_cache = ASCII_GLYPHS if mode == CharsetMode.ASCII else UNICODE_GLYPHS
    return _glyphs_cache


def is_ascii_mode() -> bool:
    return _detect_charset_mode() == CharsetMode.ASCII


def newline_shortcut() -> str:
    """Return terminal-appropriate label for newline shortcut (Option+Enter on Mac, Ctrl+J elsewhere)."""
    return "Option+Enter" if sys.platform == "darwin" else "Ctrl+J"


__all__ = [
    "ASCII_GLYPHS",
    "CharsetMode",
    "Glyphs",
    "Settings",
    "UNICODE_GLYPHS",
    "get_glyphs",
    "get_settings",
    "is_ascii_mode",
    "newline_shortcut",
    "parse_shell_allow_list",
    "reload_from_store",
    "reload_settings",
    "resolve_env_var",
    "settings",
    "sync_aws_env_aliases",
]
